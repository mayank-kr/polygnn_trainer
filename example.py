import pandas as pd

pd.options.mode.chained_assignment = None
import numpy as np
import torch

dtype = torch.cuda.FloatTensor
from torch import nn
import numpy as np
import random
from tqdm import tqdm
import polygnn_trainer as pt
from skopt import gp_minimize
from sklearn.model_selection import train_test_split
from os import mkdir
import time
import argparse
from rdkit import Chem
from rdkit.Chem import AllChem
from torch_geometric.data import Data

parser = argparse.ArgumentParser()
parser.add_argument("--device", choices=["cpu", "gpu"], default="gpu")
args = parser.parse_args()

# #########
# constants
# #########
RANDOM_SEED = 100
HP_EPOCHS = 20
SUBMODEL_EPOCHS = 100
N_FOLDS = 3
HP_NCALLS = 10
MAX_BATCH_SIZE = 50
capacity_ls = list(range(2, 6))
weight_decay = 0
PROPERTY_GROUPS = {
    "electronic": [
        "Egc",
        "Egb",
        "Ea",
        "Ei",
    ],
}
N_FEATURES = 512
OPT_CAPACITY = 2  # optimal capacity
########

start = time.time()

# fix random seeds
random.seed(RANDOM_SEED)
torch.manual_seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

# Choose the device to train our models on.
if args.device == "cpu":
    device = "cpu"
elif args.device == "gpu":
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # specify GPU

# Load data. This data set is a subset of the data used to train the
# electronic-properties MT models shown in the companion paper. The full
# data set can be found at khazana.gatech.edu.
master_data = pd.read_csv("./sample_data/sample.csv", index_col=0)
# The sample data does not contain any graph features.
master_data["graph_feats"] = [{}] * len(master_data)
# Split the data.
train_data, test_data = train_test_split(
    master_data,
    test_size=0.2,
    stratify=master_data.prop,
    random_state=RANDOM_SEED,
)
assert len(train_data) > len(test_data)


def morgan_featurizer(smile):
    smile = smile.replace("*", "H")
    mol = Chem.MolFromSmiles(smile)
    fp = AllChem.GetMorganFingerprintAsBitVect(
        mol, radius=2, nBits=N_FEATURES, useChirality=True
    )
    fp = np.expand_dims(fp, 0)
    return Data(x=torch.tensor(fp, dtype=torch.float))


# Make a directory to save our models in.
mkdir("example_models/")

# Train one model per group. We only have one group, "electronic", in this
# example file.
for group in PROPERTY_GROUPS:
    prop_cols = sorted(PROPERTY_GROUPS[group])
    print(
        f"Working on group {group}. The following properties will be modeled: {prop_cols}",
        flush=True,
    )

    nprops = len(prop_cols)
    if nprops == 1:
        selector_dim = 0
    else:
        selector_dim = nprops
    # Define a directory to save the models for this group of properties.
    root_dir = "example_models/" + group

    group_train_data = train_data.loc[train_data.prop.isin(prop_cols), :]
    group_test_data = test_data.loc[test_data.prop.isin(prop_cols), :]
    ######################
    # prepare data
    ######################
    group_train_inds = group_train_data.index.values.tolist()
    group_test_inds = group_test_data.index.values.tolist()
    group_data = pd.concat([group_train_data, group_test_data], ignore_index=False)
    group_data, scaler_dict = pt.prepare.prepare_train(
        group_data, smiles_featurizer=morgan_featurizer, root_dir=root_dir
    )
    print([(k, str(v)) for k, v in scaler_dict.items()])
    group_train_data = group_data.loc[group_train_inds, :]
    group_test_data = group_data.loc[group_test_inds, :]

    # ###############
    # do hparams opt
    # ###############
    # split train and val data
    group_fit_data, group_val_data = train_test_split(
        group_train_data,
        test_size=0.2,
        stratify=group_train_data.prop,
        random_state=RANDOM_SEED,
    )
    fit_pts = group_fit_data.data.values.tolist()
    val_pts = group_val_data.data.values.tolist()
    print(
        f"\nStarting hp opt. Using {len(fit_pts)} data points for fitting, {len(val_pts)} data points for validation."
    )
    # create objective function
    def obj_func(x):
        hps = pt.hyperparameters.HpConfig()
        hps.set_values(
            {
                "r_learn": 10 ** x[0],
                "batch_size": x[1],
                "dropout_pct": x[2],
                "capacity": OPT_CAPACITY,
                "activation": nn.functional.leaky_relu,
            }
        )
        print("Using hyperparameters:", hps)
        tc_search = pt.train.trainConfig(
            hps=hps,
            device=device,
            amp=False,  # False since we are on T2
            multi_head=False,
            loss_obj=pt.loss.sh_mse_loss(),
        )  # trainConfig for the hp search
        tc_search.epochs = HP_EPOCHS

        model = pt.models.MlpOut(
            input_dim=N_FEATURES + selector_dim,
            output_dim=1,
            hps=hps,
        )
        val_rmse = pt.train.train_submodel(
            model,
            fit_pts,
            val_pts,
            scaler_dict,
            tc_search,
        )
        return val_rmse

    # create hyperparameter space
    hp_space = [
        (np.log10(0.0003), np.log10(0.03)),  # learning rate
        (round(0.25 * MAX_BATCH_SIZE), MAX_BATCH_SIZE),  # batch size
        (0, 0.5),  # dropout
    ]

    # obtain the optimal point in hp space
    opt_obj = gp_minimize(
        func=obj_func,  # defined offline
        dimensions=hp_space,
        n_calls=HP_NCALLS,
        random_state=RANDOM_SEED,
    )
    # create an HpConfig from the optimal point in hp space
    optimal_hps = pt.hyperparameters.HpConfig()
    optimal_hps.set_values(
        {
            "r_learn": 10 ** opt_obj.x[0],
            "batch_size": opt_obj.x[1],
            "dropout_pct": opt_obj.x[2],
            "capacity": OPT_CAPACITY,
            "activation": nn.functional.leaky_relu,
        }
    )
    print(f"Optimal hps are {opt_obj.x}")
    # clear memory
    del group_fit_data
    del group_val_data

    # ################
    # Train submodels
    # ################
    tc_ensemble = pt.train.trainConfig(
        amp=False,  # False since we are on T2
        loss_obj=pt.loss.sh_mse_loss(),
        hps=optimal_hps,
        device=device,
        multi_head=False,
    )  # trainConfig for the ensemble step
    tc_ensemble.epochs = SUBMODEL_EPOCHS
    print(f"\nTraining ensemble using {len(group_train_data)} data points.")
    pt.train.train_kfold_ensemble(
        dataframe=group_train_data,
        model_constructor=lambda: pt.models.MlpOut(
            input_dim=N_FEATURES + selector_dim,
            output_dim=1,
            hps=optimal_hps,
        ),
        train_config=tc_ensemble,
        submodel_trainer=pt.train.train_submodel,
        augmented_featurizer=None,
        scaler_dict=scaler_dict,
        root_dir=root_dir,
        n_fold=N_FOLDS,
        random_seed=RANDOM_SEED,
    )
    ##########################################
    # Load and evaluate ensemble on test data
    ##########################################
    print("\nRunning predictions on test data", flush=True)
    ensemble = pt.load.load_ensemble(
        root_dir,
        pt.models.MlpOut,
        device,
        {
            "input_dim": N_FEATURES + selector_dim,
            "output_dim": 1,
        },
    )
    # remake "group_test_data" so that "graph_feats" contains dicts not arrays
    group_test_data = test_data.loc[
        test_data.prop.isin(prop_cols),
        :,
    ]
    y, y_mean_hat, y_std_hat, _selectors = pt.infer.eval_ensemble(
        model=ensemble,
        root_dir=root_dir,
        dataframe=group_test_data,
        smiles_featurizer=morgan_featurizer,
        device=device,
        ensemble_kwargs_dict={"monte_carlo": False},
    )
    pt.utils.mt_print_metrics(
        y, y_mean_hat, _selectors, scaler_dict, inverse_transform=False
    )
    print(f"Done working on group {group}\n", flush=True)

end = time.time()
print(f"Done with everything in {end-start} seconds.", flush=True)
