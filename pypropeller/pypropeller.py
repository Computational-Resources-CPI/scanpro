import time
import pandas as pd
import numpy as np
from statsmodels.stats.multitest import multipletests
import warnings

from pypropeller.get_transformed_props import get_transformed_props
from pypropeller.linear_model import lm_fit, contrasts_fit, create_design
from pypropeller import ebayes
from pypropeller.sim_reps import generate_reps, combine, get_mean_sim
from pypropeller.result import PyproResult


def pypropeller(data, clusters_col='cluster', samples_col='sample', conds_col='group',
                transform='logit', robust=True, n_sims=100, n_reps=8, verbose=True):
    """Wrapper function for pypropeller. The data must have replicates,
    since propeller requires replicated data to run. If the data doesn't have
    replicates, the function {sim_pypropeller} will generate artificial replicates
    using bootstrapping and run propeller multiple times. The values are then pooled
    to get robust estimation of p values.

    :param anndata.AnnData or pandas.DataFrame data: Single cell data with columns containing sample,
    condition and cluster/celltype information.
    :param str clusters_col: Name of column in date or data.obs where cluster/celltype information are stored, defaults to 'cluster'.
    :param str samples_col: Column in data or data.obs where sample informtaion are stored, defaults to 'sample'.
    :param str conds_col: Column in data or data.obs where condition informtaion are stored, defaults to 'group'.
    :param str transform: Method of transformation of proportions, defaults to 'logit'.
    :param bool robust: Robust ebayes estimation to mitigate the effect of outliers, defaults to True.
    :param int n_sims: Number of simulations to perform if data does not have replicates, defaults to 100.
    :param int n_reps: Number of replicates to simulate if data does not have replicates, defaults to 8.
    :param bool verbose: defaults to True.
    :raises ValueError: Data must have at least two conditions!
    :return _type_: Dataframe containing estimated mean proportions for each cluster and p-values.
    """

    if type(data).__name__ == "AnnData":
        data = data.obs

    # check if there are 2 conditions or more
    conditions = data[conds_col].unique()
    if len(conditions) < 2:
        raise ValueError("There has to be at least two conditions to compare!")

    baseline_props = data[clusters_col].value_counts() / data.shape[0]  # proportions of each cluster in all samples

    # check if there are replicates
    if len(conditions) == len(data[samples_col].unique()):
        if verbose:
            print("Your data doesn't have replicates! Artificial replicates will be simulated to run pypropeller")
            print("Simulation may take some minutes...")
        # set transform to arcsin, since it produces more accurate results for simulations
        transform = 'arcsin'
        out = sim_pypropeller(data, n_reps=n_reps, n_sims=n_sims, clusters_col=clusters_col,
                              samples_col=samples_col, conds_col=conds_col, transform=transform, robust=robust, verbose=verbose)
    else:
        out = run_pypropeller(data, clusters_col, samples_col, conds_col, transform, robust, verbose)

    columns = list(out.results.columns)
    # add baseline proportions as first columns
    out.results['Baseline_props'] = baseline_props.values
    columns.insert(0, 'Baseline_props')

    # rearrange dataframe columns
    out.results = out.results[columns]

    return out


def run_pypropeller(adata, clusters='cluster', sample='sample', cond='group', transform='logit', robust=True, verbose=True):
    """Test the significance of changes in cell proportions across conditions in single-cell data. The function
    uses empirical bayes to moderate statistical tests to give robust estimation of significance.

    :param anndata.AnnData adata: Anndata object containing single-cell data.
    :param str clusters: Column in adata.obs where cluster or celltype information are stored, defaults to 'cluster'
    :param str sample: Column in adata.obs where sample informtaion are stored, defaults to 'sample'
    :param str cond: Column in adata.obs where condition informtaion are stored, defaults to 'group'
    :param str transform: Method of normalization of proportions (logit or arcsin), defaults to 'logit'
    :param bool robust: Robust ebayes estimation to mitigate the effect of outliers, defaults to True
    :return pandas.DataFrame: Dataframe containing estimated mean proportions for each cluster and p-values.
    """
    # check data type
    if type(adata).__name__ == "AnnData":
        adata = adata.obs

    # calculate proportions and transformed proportions
    counts, props, prop_trans = get_transformed_props(adata, sample_col=sample, cluster_col=clusters, transform=transform)

    # create design matrix
    design = create_design(data=adata, samples=sample, conds=cond, reindex=props.index)

    # check number of conditions
    if design.shape[1] == 2:
        if verbose:
            print("There are 2 conditions. T-Test will be performed...")
        contrasts = [1, -1]  # columns in design matrix to be tested
        out = t_test(props, prop_trans, design, contrasts, robust)
    elif design.shape[1] > 2:
        if verbose:
            print("There are more than 2 conditions. ANOVA will be performed...")
        coef = np.arange(len(design.columns))  # columns of the design matrix corresponding to conditions of interest
        out = anova(props, prop_trans, design, coef, robust)

    if verbose:
        print('Done!')

    # create PyproResult object
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, message="Pandas doesn't allow columns to be created via a new attribute name*")

        output_obj = PyproResult()
        output_obj.results = out
        output_obj.counts = counts
        output_obj.props = props
        output_obj.prop_trans = prop_trans
        output_obj.design = design

    return output_obj


def anova(props, prop_trans, design, coef, robust=True, verbose=True):
    """Test the significance of changes in cell proportion across 3 or more conditions using
    empirical bayes and moderated ANOVA.

    :param pandas.DataFrame props: True cell proportions.
    :param pandas.DataFrame prop_trans: Normalized cell proportions.
    :param pandas.DataFrame design: Design matrix where rows are samples and columns are
    coefficients of condtions of interest to be estimated.
    :param numpy.ndarray coef: Array specifiying columns of interest in the design matrix.
    :param bool robust: Robust empirical bayes estimation of posterior variances.
    :return pandas.DataFrame: Dataframe containing estimated mean proportions for each condition,
    F-statistics, p-values and adjusted p-values.
    """
    from statsmodels.tools.tools import add_constant
    # check if coef is a numpy array
    if not isinstance(coef, np.ndarray):
        coef = np.array(coef)

    # check if there are less than 3 clusters
    if prop_trans.shape[1] < 3:
        if verbose:
            print("Robust eBayes needs 3 or more clusters! Normal eBayes will be performed")
        robust = False

    X = design.iloc[:, coef]
    # fit linear model to each cluster to get coefficients estimates
    fit_prop = lm_fit(X=X, y=props)

    # Change design matrix to intercept format
    design_2 = design.iloc[:, 1:]
    design_2 = add_constant(design_2, prepend=True, has_constant='skip')
    # fit fit linear model with all confounding variables
    fit = lm_fit(X=design_2, y=prop_trans)

    # remove intercept from stdev, coefficients and covariance matrix for the ebayes method
    fit['coefficients'] = fit['coefficients'][:, coef[1:]]
    fit['stdev'] = fit['stdev'][:, coef[1:]]
    fit['cov_coef'] = fit['cov_coef'][coef[1:][:, np.newaxis], coef[1:]]

    # get F statistics using eBayes
    fit = ebayes.ebayes(fit, robust=robust)

    # adjust p_values using benjamin hochberg method
    p_values = fit['F']['F_p_value'].flatten()
    fdr = multipletests(p_values, method='fdr_bh')

    # save results to dictionary
    res = {}
    res['Clusters'] = props.columns.to_list()
    for i, cond in enumerate(X.columns):
        res['Mean_props_' + cond] = fit_prop['coefficients'].T[i]
    res['F_statistics'] = fit['F']['stat'].flatten()
    res['p_values'] = p_values
    res['Adjusted_p_values'] = fdr[1]
    cols = list(res.keys())

    return pd.DataFrame(res, columns=cols).set_index('Clusters')


def t_test(props, prop_trans, design, contrasts, robust=True, verbose=True):
    """Test the significance of changes in cell proportion across 2 conditions using
    empirical bayes and moderated t-test.

    :param pandas.DataFrame props: True cell proportions.
    :param pandas.DataFrame prop_trans: Normalized cell proportions.
    :param pandas.DataFrame design: Design matrix where rows are samples and columns are
    coefficients of condtions of interest to be estimated.
    :param list contrasts: A list specifiying 2 conditions in the design matrix to be tested; [1, -1].
    :param bool robust: Robust empirical bayes estimation of posterior variances.
    :return pandas.DataFrame: Dataframe containing estimated mean proportions for each condition,
    F-statistics, p-values and adjusted p-values.
    """
    # check if there are less than 3 clusters
    if prop_trans.shape[1] < 3:
        if verbose:
            print("Robust eBayes needs 3 or more clusters! Normal eBayes will be performed")
        robust = False

    # fit linear model to each cluster to get coefficients estimates
    fit = lm_fit(X=design, y=prop_trans)
    fit_cont = contrasts_fit(fit, contrasts)

    # run empirical bayes
    fit_cont = ebayes.ebayes(fit_cont, robust=robust)

    # Get mean cell type proportions and relative risk for output
    # If no confounding variable included in design matrix
    contrasts = np.array(contrasts)
    if len(contrasts) == 2:
        fit_prop = lm_fit(X=design, y=props)
        # z = np.array(list(map(lambda x: x**contrasts ,fit_prop['coefficients']))).T
        z = (fit_prop['coefficients']**contrasts).T
        RR = np.prod(z, axis=0)
    # If confounding variables included in design matrix exclude them
    else:
        design = design.iloc[:, np.where(contrasts != 0)[0]]
        fit_prop = lm_fit(X=design, y=props)
        new_cont = contrasts[contrasts != 0]
        z = (fit_prop['coefficients']**new_cont).T
        RR = np.prod(z, axis=0)

    # adjust p_values using benjamin hochberg method
    p_values = fit_cont['p_value'].flatten()
    fdr = multipletests(p_values, method='fdr_bh')

    # save results to dictionary
    res = {}
    res['Clusters'] = props.columns.to_list()
    for i, cond in enumerate(design.columns):
        res['Mean_props_' + cond] = fit_prop['coefficients'].T[i]

    res['Prop_ratio'] = RR
    res['t_statistics'] = fit_cont['t'].flatten()
    res['p_values'] = p_values
    res['Adjusted_p_values'] = fdr[1]
    cols = list(res.keys())

    return pd.DataFrame(res, columns=cols).set_index('Clusters')


def sim_pypropeller(data, n_reps=8, n_sims=100, clusters_col='cluster',
                    samples_col='sample', conds_col='group', transform='arcsin', robust=True, verbose=True):
    """Run pypropeller multiple times on same dataset and pool estimates together.

    :param _type_ data: _description_
    :param int n_reps: _description_, defaults to 8
    :param int n_sims: _description_, defaults to 100
    :param str clusters_col: _description_, defaults to 'cluster'
    :param str samples_col: _description_, defaults to 'sample'
    :param str conds_col: _description_, defaults to 'group'
    :param str transform: _description_, defaults to 'arcsin'
    :param bool robust: _description_, defaults to True
    :param bool verbose: _description_, defaults to True
    """
    # check datas type
    if type(data).__name__ == "AnnData":
        data = data.obs

    conditions = data[conds_col].unique()
    n_conds = len(conditions)
    # initiate lists to save results
    res = {}
    n_clusters = len(data[clusters_col].unique())
    coefficients = {condition: np.zeros((n_sims, n_clusters)) for condition in conditions}
    p_values = np.zeros((n_sims, n_clusters))
    counts_list = []
    props_list = []
    prop_trans_list = []

    if verbose:
        print(f'Generating {n_reps} replicates and running {n_sims} simulations...')

    # start timer
    start = time.time()
    for i in range(n_sims):
        # generate replicates
        rep_data = generate_reps(data=data, n_reps=n_reps, sample_col=samples_col)
        # run propeller
        out_sim = run_pypropeller(rep_data, clusters=clusters_col, sample=samples_col,
                                  cond=conds_col, transform=transform, robust=robust, verbose=False)
        # save counts, props and prop_trans
        counts_list.append(out_sim.counts)
        props_list.append(out_sim.props)
        prop_trans_list.append(out_sim.prop_trans)

        # get adjusted p values for simulation
        p_values[i] = out_sim.results.iloc[:, -1].to_list()
        # get coefficients estimates from linear model fit
        for k, cluster in enumerate(out_sim.results.index):
            for j, condition in enumerate(conditions):
                coefficients[condition][i, k] = out_sim.results.iloc[k, j]
    # end timer
    end = time.time()
    elapsed = end - start
    if verbose:
        print(f"Finished {n_sims} simulations in {round(elapsed, 2)} seconds")

    # save design matrix
    design_sim = out_sim.design

    # combine coefficients
    combined_coefs = combine(fit=coefficients, conds=conditions, n_clusters=n_clusters, n_conds=n_conds, n_sims=n_sims)

    # get mean counts, proportions and transformed proportions from all simulations
    counts_mean = get_mean_sim(counts_list)
    props_mean = get_mean_sim(props_list)
    prop_trans_mean = get_mean_sim(prop_trans_list)

    # get clusters names
    res['Clusters'] = list(out_sim.results.index)
    for i, condition in enumerate(coefficients.keys()):
        res['Mean_props_' + condition] = combined_coefs[i]
    # calculate median of p values from all runs
    res['p_values'] = np.median(p_values, axis=0)

    # create dataframe for results
    out = pd.DataFrame(res).set_index('Clusters')

    # create PyproResult object
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, message="Pandas doesn't allow columns to be created via a new attribute name*")

        output_obj = PyproResult()
        output_obj.results = out
        output_obj.counts = counts_mean
        output_obj.props = props_mean
        output_obj.prop_trans = prop_trans_mean
        output_obj.design = design_sim

    return output_obj
